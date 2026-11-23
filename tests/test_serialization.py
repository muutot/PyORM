from copy import deepcopy
from datetime import UTC, datetime
from enum import IntEnum

from bson import BSON, Binary, Code, Decimal128, Int64, ObjectId, Regex, Timestamp

from pyorm.serialization import copy_bson, encode_value


def test_bson_copy_preserves_driver_types_aliases_and_mutable_leaf_isolation():
    shared = [{"n": 1}]
    script = Code("return n", scope={"n": [1]})
    value = {
        "left": shared,
        "right": shared,
        "script": script,
        "oid": ObjectId(),
        "binary": Binary(b"value", subtype=128),
        "integer": Int64(1),
        "decimal": Decimal128("1.23"),
        "timestamp": Timestamp(123, 1),
        "regex": Regex("abc", "i"),
        "date": datetime(2026, 1, 1, tzinfo=UTC),
    }
    copied = copy_bson(value)
    assert BSON.encode(copied) == BSON.encode(deepcopy(value))
    assert all(type(copied[key]) is type(item) for key, item in value.items())
    assert copied["left"] is copied["right"] and copied["left"] is not shared
    shared[0]["n"] = 2
    script.scope["n"].append(2)
    assert copied["left"] == [{"n": 1}] and copied["script"].scope == {"n": [1]}


def test_bson_copy_preserves_cycles_and_custom_deepcopy_behavior():
    cycle = []
    cycle.append(cycle)
    copied = copy_bson(cycle)
    assert copied is not cycle and copied[0] is copied

    class CustomList(list):
        def __deepcopy__(self, memo):
            return CustomList(["custom"])

    assert type(copy_bson(CustomList([1]))) is CustomList
    assert copy_bson(CustomList([1])) == ["custom"]


def test_scalar_fast_path_preserves_enum_and_subclass_conversion():
    class State(IntEnum):
        READY = 1

    class CustomInt(int):
        def to_mongo(self):
            return {"custom": int(self)}

    assert encode_value([State.READY, CustomInt(2), 3]) == ["READY", {"custom": 2}, 3]
