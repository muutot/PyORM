"""BSON conversion and conflict-free recursive update generation."""

from collections.abc import Mapping
from enum import Enum

from bson import BSON
from bson.errors import InvalidDocument

from .exceptions import ValidationError


def key_name(key):
    if isinstance(key, Enum):
        key = key.name
    elif isinstance(key, int) and not isinstance(key, bool):
        key = str(key)
    if not isinstance(key, str) or not key or "." in key or "$" in key or "\0" in key:
        raise ValidationError(f"Invalid MongoDB field key: {key!r}")
    return key


def encode_value(value):
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
            result[key] = encode_value(item)
        return result
    if isinstance(value, (set, frozenset)):
        values = [encode_value(item) for item in value]
        # Stable ordering means unchanged sets never generate spurious array updates.
        return sorted(values, key=lambda item: BSON.encode({"v": item}))
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    return value


def validate_bson(document):
    try:
        BSON.encode(document)
    except (InvalidDocument, OverflowError, TypeError, ValueError) as exc:
        raise ValidationError(f"Document cannot be encoded as BSON: {exc}") from exc


def collect_set_paths(value, path):
    """Locate set fields before BSON turns them into indistinguishable arrays."""
    if isinstance(value, (set, frozenset)):
        return {path}
    paths = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            paths.update(collect_set_paths(item, f"{path}.{key_name(key)}"))
    elif hasattr(value, "_fields"):
        for name, field in value._fields.items():
            if name in value._values:
                paths.update(collect_set_paths(value._values[name], f"{path}.{field.db_field}"))
    # Changes inside an array update its enclosing array; no child operations.
    return paths


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
            old_members = {BSON.encode({"v": item}): item for item in old}
            new_members = {BSON.encode({"v": item}): item for item in new}
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
            and BSON.encode({"v": old}) == BSON.encode({"v": new[: len(old)]})
        ):
            append_values(old, new[len(old) :], path)
        # Python equates 1, True and 1.0 even inside arrays; BSON distinguishes them.
        elif BSON.encode({"v": old}) != BSON.encode({"v": new}):
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
