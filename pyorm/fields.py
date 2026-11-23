"""Declarative fields, including nested documents and Python container restoration."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from enum import Enum
from typing import TYPE_CHECKING, Any, Self, overload

from .exceptions import ValidationError
from .serialization import encode_value, key_name
from .tracking import track

if TYPE_CHECKING:
    from .model import EmbeddedDocument


class _Missing:
    pass


MISSING = _Missing()


class Field[T = Any]:
    def __init__(
        self,
        python_type: type[T] | tuple[type[Any], ...] | None = None,
        *,
        default: T | _Missing = MISSING,
        default_factory: Callable[[], T] | None = None,
        required: bool = False,
        nullable: bool = True,
        db_field: str | None = None,
        validator: Callable[[T], bool | None] | None = None,
    ) -> None:
        if default is not MISSING and default_factory is not None:
            raise ValueError("Use either default or default_factory")
        self.python_type = python_type
        self.default = default
        self.default_factory = default_factory
        self.required = required
        self.nullable = nullable
        self.db_field: str = db_field or ""
        self.validator = validator
        self.name: str = ""

    def __set_name__(self, owner: type[EmbeddedDocument], name: str) -> None:
        self.name = name
        self.db_field = key_name(self.db_field or name)

    def get_default(self) -> T | _Missing:
        if self.default_factory is not None:
            return self.default_factory()
        return deepcopy(self.default) if self.default is not MISSING else MISSING

    @overload
    def __get__(self, instance: None, owner: type[EmbeddedDocument] | None = None) -> Self: ...

    @overload
    def __get__(
        self, instance: EmbeddedDocument, owner: type[EmbeddedDocument] | None = None
    ) -> T: ...

    def __get__(
        self, instance: EmbeddedDocument | None, owner: type[EmbeddedDocument] | None = None
    ) -> T | Self | None:
        if instance is None:
            return self
        return instance._values.get(self.name)

    def __set__(self, instance: EmbeddedDocument, value: T) -> None:
        with instance._lock:
            changed = instance._on_change
            if changed is not None:
                changed.before()
            self.validate(value)
            if changed is None:
                instance._values[self.name] = value
                return
            hook = instance._field_hook(self.name)
            # Tracked containers retain the declared native-container interface.
            instance._values[self.name] = track(value, hook, instance._lock)
            hook()  # pyright: ignore[reportOptionalCall]

    def __delete__(self, instance: EmbeddedDocument) -> None:
        with instance._lock:
            changed = instance._on_change
            if changed is not None:
                changed.before()
            if self.required:
                raise ValidationError(f"Field {self.name!r} is required")
            instance._values.pop(self.name, None)
            if changed is not None:
                instance._field_hook(self.name)()  # pyright: ignore[reportOptionalCall]

    def validate(self, value) -> None:
        if value is None:
            if self.required or not self.nullable:
                raise ValidationError(f"Field {self.name!r} cannot be None")
            return
        if self.python_type is not None and (
            not isinstance(value, self.python_type)
            or (self.python_type in (int, float) and isinstance(value, bool))
        ):
            raise ValidationError(f"Field {self.name!r} must be {self.python_type!r}")
        if self.validator is not None and self.validator(value) is False:
            raise ValidationError(f"Field {self.name!r} failed validation")

    def to_mongo(self, value: T):
        self.validate(value)
        return encode_value(value)

    def from_mongo(self, value) -> T | None:
        if value is not None and self.python_type is not None:
            target = self.python_type
            if isinstance(target, type) and issubclass(target, Enum):
                try:
                    value = target[value]
                except (KeyError, TypeError) as exc:
                    raise ValidationError(f"Invalid enum for {self.name!r}: {value!r}") from exc
            elif target in (set, frozenset, tuple) and isinstance(value, list):
                # BSON arrays inside a set must be restored to hashable tuples.
                def hashable(item) -> Any:
                    return tuple(hashable(x) for x in item) if isinstance(item, list) else item

                value = target(hashable(x) for x in value) if target is not tuple else tuple(value)
        self.validate(value)
        # BSON restoration is validated above; no runtime cast is needed.
        return value  # pyright: ignore[reportReturnType]


class EmbeddedField[T: "EmbeddedDocument"](Field[T]):
    def __init__(self, document_type: type[T], **kwargs) -> None:
        if "default" not in kwargs and "default_factory" not in kwargs:
            kwargs["default_factory"] = document_type
        super().__init__(document_type, **kwargs)
        self.document_type = document_type

    def from_mongo(self, value) -> T | None:
        if value is not None:
            value = self.document_type.from_mongo(value)
        self.validate(value)
        return value


class ListField[T = Any](Field[list[T]]):
    def __init__(self, item_field: Field[T] | None = None, **kwargs) -> None:
        if "default" not in kwargs and "default_factory" not in kwargs:
            kwargs["default_factory"] = list
        super().__init__(list, **kwargs)
        self.item_field = item_field

    def validate(self, value) -> None:
        super().validate(value)
        if value is not None and self.item_field is not None:
            for item in value:
                self.item_field.validate(item)

    def from_mongo(self, value) -> list[T] | None:
        if value is not None and self.item_field is not None:
            value = [self.item_field.from_mongo(item) for item in value]
        self.validate(value)
        return value  # pyright: ignore[reportReturnType]
