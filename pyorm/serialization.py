"""BSON conversion and conflict-free recursive update generation."""

from collections.abc import Mapping
from copy import deepcopy
from enum import Enum

from bson import encode as encode_bson
from bson.errors import InvalidDocument

from ._acceleration import native
from .exceptions import ValidationError
from .tracking import TrackedDict, TrackedList

_SCALAR_TYPES = frozenset((str, int, float, bool, bytes, type(None)))


def _copy_bson_python[T](value: T, memo=None) -> T:
    """Detach BSON trees cheaply, retaining deepcopy semantics for driver/custom values."""
    kind = type(value)
    if kind in _SCALAR_TYPES:
        return value
    if kind not in (dict, list):
        return deepcopy(value, memo)
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if kind is list:
        result = []
        memo[identity] = result
        result.extend(
            item if type(item) in _SCALAR_TYPES else _copy_bson_python(item, memo) for item in value
        )
    else:
        result = {}
        memo[identity] = result
        for key, item in value.items():
            result[_copy_bson_python(key, memo)] = (
                item if type(item) in _SCALAR_TYPES else _copy_bson_python(item, memo)
            )
    # Only exact native lists/dicts take this branch; all subclasses use deepcopy.
    return result  # pyright: ignore[reportReturnType]


def copy_bson[T](value: T, memo=None) -> T:
    """Detach BSON trees while preserving subclasses, aliases, and custom copying."""
    if type(value) in _SCALAR_TYPES:
        return value
    if native is not None and (memo is None or type(memo) is dict):
        return native.copy_bson(value, {} if memo is None else memo, deepcopy)
    return _copy_bson_python(value, memo)


def key_name(key):
    if isinstance(key, Enum):
        key = key.name
    elif isinstance(key, int) and not isinstance(key, bool):
        key = str(key)
    if not isinstance(key, str) or not key or "." in key or "$" in key or "\0" in key:
        raise ValidationError(f"Invalid MongoDB field key: {key!r}")
    return key


def _encode_value_python(value):
    if type(value) in _SCALAR_TYPES:
        return value
    if isinstance(value, Enum):
        return value.name
    if hasattr(value, "to_mongo"):
        return value.to_mongo()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key = key_name(key)
            if key in result:
                raise ValidationError(f"Dictionary keys collide after BSON conversion: {key!r}")
            result[key] = _encode_value_python(item)
        return result
    if isinstance(value, (set, frozenset)):
        values = [_encode_value_python(item) for item in value]
        # Stable ordering means unchanged sets never generate spurious array updates.
        return sorted(values, key=lambda item: encode_bson({"v": item}))
    if isinstance(value, (list, tuple)):
        return [
            item if type(item) in _SCALAR_TYPES else _encode_value_python(item) for item in value
        ]
    return value


def _encode_fallback(value):
    if (
        native is not None
        and isinstance(value, (set, frozenset))
        and not hasattr(value, "to_mongo")
    ):
        values = [encode_value(item) for item in value]
        return native.sort_set(encode_bson({"v": values}), values)
    return _encode_value_python(value)


def encode_value(value):
    if type(value) in _SCALAR_TYPES:
        return value
    if native is not None:
        return native.encode_value(value, _encode_fallback, key_name, TrackedDict, TrackedList)
    return _encode_value_python(value)


def validate_bson(document):
    try:
        encode_bson(document)
    except (InvalidDocument, OverflowError, TypeError, ValueError) as exc:
        raise ValidationError(f"Document cannot be encoded as BSON: {exc}") from exc


def _collect_set_paths_python(value, path):
    """Locate set fields before BSON turns them into indistinguishable arrays."""
    if type(value) in _SCALAR_TYPES:
        return set()
    if isinstance(value, (set, frozenset)):
        return {path}
    paths = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            paths.update(_collect_set_paths_python(item, f"{path}.{key_name(key)}"))
    elif hasattr(value, "_fields"):
        for name, field in value._fields.items():
            if name in value._values:
                paths.update(
                    _collect_set_paths_python(value._values[name], f"{path}.{field.db_field}")
                )
    # Changes inside an array update its enclosing array; no child operations.
    return paths


def collect_set_paths(value, path):
    if native is not None:
        return native.collect_set_paths(
            value, path, _collect_set_paths_python, key_name, TrackedDict, TrackedList
        )
    return _collect_set_paths_python(value, path)


def diff_documents(before, after, *, incremental_arrays=False, set_paths=()):
    """Use dotted dict paths and optional append/add operations on existing arrays."""
    sets, unsets, pushes = {}, {}, {}

    def append_values(old, added, path):
        if len(added) == 1:
            # A fixed tail index is idempotent and avoids the $push machinery.
            sets[f"{path}.{len(old)}"] = added[0]
        elif added:
            pushes[path] = {"$each": added}

    def array_diff(old, new, path):
        if incremental_arrays and path in set_paths:
            if native is not None:
                indices = native.set_added(encode_bson({"v": old}), encode_bson({"v": new}))
                if indices is None:
                    sets[path] = new
                else:
                    append_values(old, [new[index] for index in indices], path)
                return
            old_members = {encode_bson({"v": item}): item for item in old}
            new_members = {encode_bson({"v": item}): item for item in new}
            removed = old_members.keys() - new_members.keys()
            if removed or len(old_members) != len(old):
                sets[path] = new
            else:
                added = [item for key, item in new_members.items() if key not in old_members]
                # The authoritative cache already deduplicates set members. Avoid
                # making MongoDB scan the stored array again with $addToSet.
                append_values(old, added, path)
        elif (
            incremental_arrays
            and len(new) > len(old)
            and (
                native.array_prefix(encode_bson({"v": old}), encode_bson({"v": new}))
                if native is not None
                else encode_bson({"v": old}) == encode_bson({"v": new[: len(old)]})
            )
        ):
            append_values(old, new[len(old) :], path)
        # Python equates 1, True and 1.0 even inside arrays; BSON distinguishes them.
        elif encode_bson({"v": old}) != encode_bson({"v": new}):
            sets[path] = new

    def walk(old, new, prefix):
        for key in old.keys() - new.keys():
            unsets[f"{prefix}{key}"] = ""
        for key, value in new.items():
            path = f"{prefix}{key}"
            if key not in old:
                sets[path] = value
            elif isinstance(old[key], dict) and isinstance(value, dict):
                walk(old[key], value, path + ".")
            elif isinstance(value, list) and isinstance(old[key], list):
                array_diff(old[key], value, path)
            elif type(old[key]) is not type(value) or old[key] != value:
                sets[path] = value

    walk(before, after, "")
    update = {}
    if sets:
        update["$set"] = sets
    if unsets:
        update["$unset"] = unsets
    if pushes:
        update["$push"] = pushes
    return update
